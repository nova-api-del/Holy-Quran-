import importlib.util
import os
import re
import tempfile
import time
from pathlib import Path
from difflib import SequenceMatcher

import requests

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "work" / "models" / "quran-asr-small"
MODEL_REVISION = "0c2744b9d70aaf45ba574c30c5d318c575258e66"
HF_BASE = f"https://huggingface.co/Abdou/quran-asr-small/resolve/{MODEL_REVISION}"
MODEL_FILES = {
    "model.int8.onnx": f"{HF_BASE}/model.int8.onnx",
    "tokens.txt": f"{HF_BASE}/tokens.txt",
    "retrieve.py": f"{HF_BASE}/retrieve.py",
}


def _download(url, path, minimum_bytes=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= minimum_bytes:
        return
    tmp = path.with_suffix(path.suffix + ".part")
    print(f"Downloading Quran ASR asset: {path.name}")
    with requests.get(url, stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        with tmp.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    out.write(chunk)
    if tmp.stat().st_size < minimum_bytes:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Quran ASR asset is incomplete: {path.name}")
    tmp.replace(path)


def _assets():
    model = MODEL_DIR / "model.int8.onnx"
    tokens = MODEL_DIR / "tokens.txt"
    retrieve = MODEL_DIR / "retrieve.py"
    _download(MODEL_FILES["model.int8.onnx"], model, 20_000_000)
    _download(MODEL_FILES["tokens.txt"], tokens, 100)
    _download(MODEL_FILES["retrieve.py"], retrieve, 2_000)
    return model, tokens, retrieve


def _load_retriever(path):
    spec = importlib.util.spec_from_file_location("quran_asr_retrieve", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the pinned Quran ayah retrieval module.")
    # The upstream retrieval script uses bisect in Quran.find; ensure it is imported
    # for the pinned upstream revision before loading the module.
    source = Path(path).read_text(encoding="utf-8")
    if "import bisect" not in source:
        source = "import bisect\n" + source
        Path(path).write_text(source, encoding="utf-8")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _word_timings(tokens, timestamps, retriever):
    if not tokens or len(tokens) != len(timestamps):
        raise RuntimeError("Quran ASR did not return token-level timestamps; refusing unsynchronised captions.")
    words = []
    current_text = []
    current_times = []

    def flush():
        if not current_text:
            return
        raw = "".join(current_text)
        normalized = retriever.letters(raw)
        if normalized and current_times:
            words.append({
                "text": normalized,
                "start": max(0.0, min(current_times)),
                "end": max(0.0, max(current_times) + 0.28),
            })
        current_text.clear()
        current_times.clear()

    for token, stamp in zip(tokens, timestamps):
        token = str(token).replace("▁", " ").replace("|", " ")
        try:
            stamp = float(stamp)
        except (TypeError, ValueError):
            continue
        parts = re.split(r"(\s+)", token)
        for part in parts:
            if not part:
                continue
            if part.isspace():
                flush()
            else:
                current_text.append(part)
                current_times.append(stamp)
    flush()
    return words


def transcribe_and_match(audio_path, duration_seconds, reciter):
    """
    Quran-specific offline ASR + canonical ayah retrieval.
    Verse timestamps are derived from CTC token timestamps and matched Quran words.
    """
    import numpy as np
    import soundfile as sf
    import sherpa_onnx

    model_path, tokens_path, retrieve_path = _assets()
    retriever = _load_retriever(retrieve_path)

    with tempfile.TemporaryDirectory(prefix="quran_asr_") as temp:
        wav = Path(temp) / "recitation_16k_mono.wav"
        import subprocess
        subprocess.run([
            "ffmpeg", "-y", "-i", str(audio_path), "-vn", "-ac", "1",
            "-ar", "16000", "-c:a", "pcm_s16le", str(wav)
        ], check=True, capture_output=True)
        samples, sample_rate = sf.read(str(wav), dtype="float32", always_2d=True)
        if sample_rate != 16000 or samples.size == 0:
            raise RuntimeError("Could not prepare the recitation for Quran ASR.")
        samples = np.asarray(samples[:, 0], dtype=np.float32)

        recognizer = sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
            model=str(model_path),
            tokens=str(tokens_path),
            num_threads=max(1, min(4, os.cpu_count() or 2)),
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
        )
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        recognizer.decode_stream(stream)
        result = stream.result
        transcript = str(getattr(result, "text", "") or "")
        raw_tokens = list(getattr(result, "tokens", []) or [])
        timestamps = list(getattr(result, "timestamps", []) or [])
        if not transcript.strip() and raw_tokens:
            transcript = "".join(str(x) for x in raw_tokens)
        heard_words = _word_timings(raw_tokens, timestamps, retriever)
        if len(heard_words) < 8:
            raise RuntimeError("Quran ASR returned too little usable recitation text.")
        heard_text = " ".join(w["text"] for w in heard_words)

        quran = retriever.Quran(cache=str(MODEL_DIR / "quran_uthmani_hafs.json"))
        match = quran.find(heard_text)
        if not match:
            raise RuntimeError("Quran ASR transcript could not be matched to the canonical Mushaf.")
        score = float(match.get("score", 0))
        print(f"Quran ASR retrieval score: {score:.1f}/100 | reciter={reciter}")
        if score < 72:
            raise RuntimeError(f"Quran ASR match confidence too low ({score:.1f}); refusing publication.")

        span = match.get("span")
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            raise RuntimeError("Quran retrieval did not return a word alignment span.")
        first_word, last_word = int(span[0]), int(span[1])
        target_words = quran.words[first_word:last_word + 1]
        if not target_words:
            raise RuntimeError("Quran retrieval returned an empty ayah span.")

        heard_norm = [w["text"] for w in heard_words]
        target_norm = [retriever.letters(w[2]) for w in target_words]
        matcher = SequenceMatcher(None, heard_norm, target_norm, autojunk=False)
        target_to_heard = {}
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                target_to_heard[block.b + offset] = block.a + offset

        ayahs = match.get("ayahs") or []
        if not ayahs:
            raise RuntimeError("Quran retrieval returned no complete ayahs.")
        verses = []
        for ayah in ayahs:
            surah = int(ayah["sura"])
            ayah_no = int(ayah["aya"])
            indices = [
                i for i, word in enumerate(target_words)
                if int(word[0]) == surah and int(word[1]) == ayah_no
            ]
            if not indices:
                continue
            mapped = [target_to_heard[i] for i in indices if i in target_to_heard]
            coverage = len(mapped) / len(indices)
            # Require both the opening and closing words: partial ayahs are never captioned.
            if coverage < 0.82 or indices[0] not in target_to_heard or indices[-1] not in target_to_heard:
                print(f"Skipping incomplete/uncertain ayah {surah}:{ayah_no} (word coverage {coverage:.0%})")
                continue
            first_heard = target_to_heard[indices[0]]
            last_heard = target_to_heard[indices[-1]]
            start = heard_words[first_heard]["start"]
            end = heard_words[last_heard]["end"]
            if not (0 <= start < end <= duration_seconds + 0.5):
                continue
            verses.append({
                "surah": surah,
                "ayah": ayah_no,
                "start_seconds": float(start),
                "end_seconds": float(min(duration_seconds, end)),
                "text": str(ayah["text"]),
            })

        verses.sort(key=lambda x: (x["surah"], x["ayah"]))
        if not verses:
            raise RuntimeError("No complete Quran ayah passed the word-level alignment check.")

        surah_numbers = {v["surah"] for v in verses}
        if len(surah_numbers) != 1:
            raise RuntimeError("The selected source crosses surah boundaries; refusing to mix surahs.")
        surah_no = next(iter(surah_numbers))
        # Keep only the longest contiguous, well-aligned run; do not join unrelated ayahs.
        runs, current = [], []
        for verse in verses:
            if current and (verse["ayah"] != current[-1]["ayah"] + 1):
                runs.append(current)
                current = []
            current.append(verse)
        if current:
            runs.append(current)
        best = max(runs, key=lambda run: (len(run), run[-1]["end_seconds"] - run[0]["start_seconds"]))
        if len(best) > 1:
            verses = best
        else:
            verses = best

        try:
            import requests as _requests
            response = _requests.get(f"https://api.alquran.cloud/v1/surah/{surah_no}", timeout=30)
            response.raise_for_status()
            surah_name = response.json()["data"]["name"]
        except Exception:
            surah_name = f"السورة {surah_no}"

        print(f"Quran ASR verified {len(verses)} complete consecutive ayahs in {surah_name}.")
        return {
            "surah": surah_no,
            "surah_name": surah_name,
            "verses": verses,
            "asr_score": score,
        }
