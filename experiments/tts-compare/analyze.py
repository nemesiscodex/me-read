"""Transcribe listening samples locally and calculate an ASR word-error estimate."""
import argparse
import json
from pathlib import Path
import re
import unicodedata


def words(text):
    return re.findall(r'[^\W_]+', unicodedata.normalize('NFC', text).casefold())


def edits(expected, heard):
    previous = list(range(len(heard) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(heard, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('outputs', type=Path)
    parser.add_argument('--whisper', type=Path, required=True)
    args = parser.parse_args()
    from mlx_audio.stt import load
    from mlx_audio.stt.utils import load_audio
    model = load(str(args.whisper))
    results = []
    for log in sorted(args.outputs.glob('*.jsonl')):
        for line in log.read_text().splitlines():
            row = json.loads(line)
            if row.get('event') != 'sample' or row.get('warmup') or row.get('repetition') != 1:
                continue
            if row.get('status') != 'ok':
                results.append(row)
                continue
            result = model.generate(load_audio(str(args.outputs / row['file']), sr=16000),
                                    language=row['language'])
            reference, heard = words(row['text']), words(result.text)
            # Recognizers format dates, prices, and abbreviations differently from spoken text.
            # Keep these passages for listening without miscounting formatting as speech errors.
            score = not row['case'].endswith('pronunciation')
            row.update(asr_text=result.text.strip(), word_errors=edits(reference, heard) if score else None,
                       reference_words=len(reference),
                       asr_wer=edits(reference, heard) / len(reference) if score else None)
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            (args.outputs / 'analysis.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
