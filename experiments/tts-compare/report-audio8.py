"""Build a blind A/B listening page: Audio8 Air against Chatterbox, from two output folders with analysis.json."""
import argparse
import html
import json
from pathlib import Path
import statistics

from report import listening_audio

CHATTERBOX = {'en': 'Chatterbox Turbo', 'es': 'Chatterbox Multilingual v3'}


def load(folder):
    analysis = json.loads((folder / 'analysis.json').read_text())
    records = [json.loads(line) for path in folder.glob('*.jsonl') for line in path.read_text().splitlines()]
    return analysis, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio8', type=Path, required=True)
    parser.add_argument('--chatterbox', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--audio8-label', default='Audio8 Air')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    escape = html.escape
    sources = {'audio8': (args.audio8, *load(args.audio8)), 'chatterbox': (args.chatterbox, *load(args.chatterbox))}
    sections, timing = [], []
    for language, title in [('en', 'English'), ('es', 'Español')]:
        reference, _ = listening_audio(args.references / f'{language}.wav')
        sections.append(f'<section data-language="{language}" {"" if language == "en" else "hidden"}>'
                        f'<details><summary>Reference voice · {title}</summary>'
                        f'<audio controls preload="none" src="{reference}"></audio></details>')
        for index, case in enumerate(['narration', 'dialogue', 'pronunciation']):
            takes = []
            for engine, (folder, analysis, _) in sources.items():
                row = next(r for r in analysis if r.get('case') == f'{language}-{case}' and r.get('engine') == engine)
                takes.append((engine, row, folder))
            if (index + (language == 'es')) % 2:
                takes.reverse()
            sections.append(f'<article><h3>{case.capitalize()}</h3><p class="script">{escape(takes[0][1]["text"])}</p><div class="pair">')
            for letter, (engine, row, folder) in zip('AB', takes):
                source, _ = listening_audio(folder / row['file'])
                name = args.audio8_label if engine == 'audio8' else CHATTERBOX[language]
                score = (f'{row["word_errors"]} word errors / {row["reference_words"]} words'
                         if row['word_errors'] is not None else 'Listen for numbers and names. Not scored.')
                sections.append(f'<div class="take"><strong>Voice {letter}</strong><span class="identity" hidden>{escape(name)}</span>'
                                f'<audio aria-label="{title} {case} voice {letter}" controls preload="none" src="{source}"></audio>'
                                f'<details class="identity" hidden><summary>Whisper transcript</summary>'
                                f'<p>{escape(row["asr_text"])}</p><p>{score}</p></details></div>')
            sections.append('</div></article>')
        sections.append('</section>')
        for engine, (folder, analysis, records) in sources.items():
            rows = [r for r in records if r.get('event') == 'sample' and r.get('case') == f'{language}-narration'
                    and r.get('engine') == engine and r.get('status') == 'ok' and not r.get('warmup')]
            scored = [r for r in analysis if r.get('language') == language and r.get('engine') == engine
                      and r.get('word_errors') is not None]
            name = args.audio8_label if engine == 'audio8' else CHATTERBOX[language]
            timing.append(f'<tr><td>{language.upper()}</td><td>{escape(name)}</td>'
                          f'<td>{statistics.median(r["seconds"] for r in rows):.1f}s / {rows[0]["audio_seconds"]:.1f}s</td>'
                          f'<td>{statistics.median(r["rtf"] for r in rows):.2f}</td>'
                          f'<td>{sum(r["word_errors"] for r in scored)}/{sum(r["reference_words"] for r in scored)}</td></tr>')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audio8 and Chatterbox listening comparison</title><style>
*{box-sizing:border-box}:root{color-scheme:light dark}body{margin:0 auto;max-width:900px;padding:16px;font:15px/1.5 system-ui;background:Canvas;color:CanvasText}
h2{font-size:22px;margin:0 0 8px}h3{font-size:17px;margin:0 0 8px}p{margin:8px 0 14px}.note{color:GrayText}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}button{font:inherit;cursor:pointer;border:1px solid GrayText;border-radius:8px;padding:8px 14px;background:ButtonFace;color:ButtonText}
button[aria-pressed=true]{outline:2px solid Highlight}.reveal{margin-left:auto}article{border-top:1px solid GrayText;padding:20px 0}.script{max-width:65ch}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}.take{padding:16px;border:1px solid GrayText;border-radius:10px;min-width:0}.identity{font-size:13px;color:GrayText}
audio{display:block;width:100%;margin-top:12px;height:38px}details{margin:12px 0}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px;text-align:left;border-bottom:1px solid GrayText}
[hidden]{display:none!important}@media(max-width:500px){.pair{grid-template-columns:1fr}.reveal{margin-left:0}}
</style><h2>Listen before revealing the models</h2>
<p>Six matched passages from two saved me-read narrators. Compare pronunciation, pauses, expression, and resemblance to the reference voice.</p>
<div class="toolbar"><button data-tab="en" aria-pressed="true">English</button><button data-tab="es" aria-pressed="false">Español</button><button id="reveal" class="reveal" aria-pressed="false">Reveal models and measurements</button></div>
''' + ''.join(sections) + '''<div class="identity" hidden><h3>What this run measured</h3><table><thead><tr><th>Language</th><th>Model</th><th>Work / audio</th><th>Work per audio second</th><th>ASR word errors</th></tr></thead><tbody>''' + ''.join(timing) + '''</tbody></table>
<p class="note">Median of five warm narration runs, seed 7, one AMD gfx1030 GPU with 16 GB, PyTorch 2.9.1 for ROCm 6.4. Each model ran alone. Timings exclude loading and reference preparation. Playback copies are matched to −20 LUFS; the WAV files are unchanged. Word errors are Whisper's disagreement with the script, not a voice-quality score.</p></div>
<script>
for(const b of document.querySelectorAll('[data-tab]'))b.onclick=()=>{for(const o of document.querySelectorAll('[data-tab]'))o.setAttribute('aria-pressed',o===b);for(const s of document.querySelectorAll('[data-language]'))s.hidden=s.dataset.language!==b.dataset.tab;for(const a of document.querySelectorAll('audio'))a.pause()};
document.getElementById('reveal').onclick=function(){const on=this.getAttribute('aria-pressed')!=='true';this.setAttribute('aria-pressed',on);this.textContent=on?'Hide models and measurements':'Reveal models and measurements';for(const e of document.querySelectorAll('.identity'))e.hidden=!on};
for(const a of document.querySelectorAll('audio'))a.addEventListener('play',()=>{for(const b of document.querySelectorAll('audio'))if(a!==b)b.pause()});
</script></html>'''
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(args.out)


if __name__ == '__main__':
    main()
