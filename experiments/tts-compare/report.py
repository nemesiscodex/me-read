"""Build a self-contained listening page from the generated audio and analysis.json."""
import argparse
import base64
import html
import json
from pathlib import Path
import statistics
import subprocess


def listening_audio(source):
    measured = subprocess.run(
        ['ffmpeg', '-hide_banner', '-i', str(source), '-af', 'loudnorm=I=-20:TP=-2:LRA=11:print_format=json',
         '-f', 'null', '-'], capture_output=True, text=True, check=True)
    fields, _ = json.JSONDecoder().raw_decode(measured.stderr[measured.stderr.rfind('{'):])
    gain = min(-20 - float(fields['input_i']), -2 - float(fields['input_tp']))
    encoded = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', str(source), '-af', f'volume={gain}dB',
         '-ar', '24000', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '64k', '-f', 'mp3', '-'],
        capture_output=True, check=True).stdout
    return 'data:audio/mpeg;base64,' + base64.b64encode(encoded).decode(), gain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('outputs', type=Path)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--runtime', choices=('rocm', 'mlx'), default='rocm')
    parser.add_argument('--timing-note', default='')
    args = parser.parse_args()
    ming_label = 'Ming 0.5B 8-bit' if args.runtime == 'mlx' else 'Ming 0.5B'
    hardware = 'Apple M1 Max with 32 GB' if args.runtime == 'mlx' else 'AMD gfx1030 GPU with 16 GB'
    exclusions = ('loading and the reader pipeline. Ming includes reference preparation on each call; '
                  'Chatterbox caches it before timing' if args.runtime == 'mlx' else
                  'loading, reference preparation, and the reader pipeline')
    method = ('Ming 0.5B 8-bit uses mlx-audio 0.6.1 in a separate environment. Cloning uses reference audio '
              'and its transcript; this port does not extract CAM++ speaker embeddings. Chatterbox uses '
              'me-read’s installed mlx-audio 0.5.7 and application settings: Turbo for English and '
              'Multilingual for Spanish. Apple M1 Max with 32 GB. Seed 7. No delivery instructions or emotion takes.'
              if args.runtime == 'mlx' else
              'Ming 0.5B uses its upstream code with speaker embeddings, the reference transcript, bfloat16, and SDPA. '
              'English uses Chatterbox Turbo; Spanish uses Chatterbox Multilingual v3. PyTorch 2.9.1 / ROCm 6.4 '
              'on an AMD gfx1030 GPU with 16 GB. Seed 7. No delivery instructions or emotion takes.')
    model_url = ('https://huggingface.co/mlx-community/Ming-3.0-tts-0.5B-8bit' if args.runtime == 'mlx' else
                 'https://huggingface.co/inclusionAI/Ming-3.0-tts-0.5B')
    analysis = json.loads((args.outputs / 'analysis.json').read_text())
    records = [json.loads(line) for path in args.outputs.glob('*.jsonl') for line in path.read_text().splitlines()]
    escape = html.escape
    sections = []
    gains = {}
    audio_urls = {}
    for language, title in [('en', 'English'), ('es', 'Español')]:
        reference, gain = listening_audio(args.references / f'{language}.wav')
        gains[f'reference-{language}'] = gain
        sections.append(f'<section data-language="{language}" {"" if language == "en" else "hidden"}>'
                        f'<details><summary>Reference voice · {title}</summary><p>Both models clone this recording.</p>'
                        f'<audio controls preload="none" src="{reference}"></audio></details>')
        for index, case in enumerate(['narration', 'dialogue', 'pronunciation']):
            pair = [row for row in analysis if row.get('case') == f'{language}-{case}']
            if len(pair) != 2 or any(row['status'] != 'ok' for row in pair):
                raise ValueError(f'Missing successful pair for {language}-{case}')
            pair.sort(key=lambda row: row['engine'], reverse=(index + (language == 'es')) % 2 == 0)
            sections.append(f'<article><h3>{case.capitalize()}</h3><p class="script">{escape(pair[0]["text"])}</p><div class="pair">')
            for letter, row in zip('AB', pair):
                source, gain = listening_audio(args.outputs / row['file'])
                audio_urls[row['file']] = source
                gains[row['file']] = gain
                model = ming_label if row['engine'] == 'ming' else ('Chatterbox Turbo' if language == 'en' else 'Chatterbox Multilingual')
                score = (f'{row["word_errors"]} word errors / {row["reference_words"]} words'
                         if row['word_errors'] is not None else 'Listen for numbers and names. ASR formatting is not scored.')
                sections.append(f'<div class="take"><strong>Voice {letter}</strong>'
                                f'<span class="identity" hidden>{model}</span>'
                                f'<audio aria-label="{title} {case} voice {letter}" controls preload="none" src="{source}"></audio>'
                                f'<details class="identity" hidden><summary>Whisper transcript</summary>'
                                f'<p>{escape(row["asr_text"])}</p><p>{score}</p>'
                                '</details></div>')
            sections.append('</div></article>')
        sections.append('</section>')

    timing = []
    summary = []
    for language in ('en', 'es'):
        for engine in ('ming', 'chatterbox'):
            rows = [r for r in records if r.get('event') == 'sample' and r.get('case') == f'{language}-narration'
                    and r.get('engine') == engine and r.get('status') == 'ok' and not r.get('warmup')]
            speeds = [r['rtf'] for r in rows]
            generation = [r['seconds'] for r in rows]
            quality = [r for r in analysis if r.get('language') == language and r.get('engine') == engine
                       and r.get('word_errors') is not None]
            errors = sum(r['word_errors'] for r in quality)
            total = sum(r['reference_words'] for r in quality)
            item = dict(language=language, engine=engine, count=len(rows), rtf_median=statistics.median(speeds),
                        rtf_min=min(speeds), rtf_max=max(speeds), generation_median=statistics.median(generation),
                        audio_seconds=rows[0]['audio_seconds'], word_errors=errors, reference_words=total,
                        asr_wer=errors / total)
            summary.append(item)
            label = 'Ming' if engine == 'ming' else 'Chatterbox'
            timing.append(f'<tr><td>{language.upper()}</td><td>{label}</td>'
                          f'<td>{item["generation_median"]:.2f}s / {item["audio_seconds"]:.2f}s</td>'
                          f'<td>{item["rtf_median"]:.2f} <small>({min(speeds):.2f}–{max(speeds):.2f})</small></td>'
                          f'<td>{errors}/{total}</td></tr>')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ming and Chatterbox listening comparison</title><style>
*{box-sizing:border-box}:root{color-scheme:light dark}body{margin:0;font:15px/1.5 var(--font-sans,system-ui);color:var(--foreground,CanvasText);background:transparent}
h2{font-size:22px;margin:0 0 8px;font-weight:650}h3{font-size:17px;margin:0 0 8px}p{margin:8px 0 14px}small,.note{color:var(--muted-foreground,#999)}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:18px 0}button{font:inherit;cursor:pointer;border:1px solid var(--border,#888);border-radius:8px;background:var(--secondary,ButtonFace);color:inherit;padding:8px 14px}
button[aria-pressed=true]{background:var(--primary,#e7e7e7);color:var(--primary-foreground,#181818)}.reveal{margin-left:auto}
article{border-top:1px solid var(--border,#555);padding:20px 0}.script{max-width:65ch}.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.take{padding:16px;background:var(--card,Canvas);border:1px solid var(--border,#888);border-radius:var(--radius,10px);min-width:0}.take strong{display:inline-block;margin-right:10px}.identity{font-size:13px;color:var(--muted-foreground,GrayText)}
audio{display:block;width:100%;margin-top:12px;height:38px}details{margin:12px 0}summary{cursor:pointer}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px 7px;text-align:left;border-bottom:1px solid var(--border,#555)}th{font-weight:600}td small{white-space:nowrap}.scroll{overflow:auto}
[hidden]{display:none!important}@media(max-width:500px){.pair{grid-template-columns:1fr}.reveal{margin-left:0}th,td{padding:8px 4px}}
</style><h2>Listen before revealing the models</h2>
<p>Six matched passages, two saved me-read narrators. Compare pronunciation, pauses, expression, and resemblance to the reference voice.</p>
<div class="toolbar"><button data-tab="en" aria-pressed="true">English</button><button data-tab="es" aria-pressed="false">Español</button><button id="reveal" class="reveal" aria-pressed="false">Reveal models and measurements</button></div>
''' + ''.join(sections) + '''<div class="identity" hidden><h3>What this run measured</h3><div class="scroll"><table><thead><tr><th>Language</th><th>Model</th><th>Work / audio</th><th>Seconds of work per audio second</th><th>ASR word errors</th></tr></thead><tbody>''' + ''.join(timing) + '''</tbody></table></div>
<p class="note">Work and ratio show the median of five warm narration runs, with the ratio range in brackets. Lower ratios mean less waiting. Ratios below 1 generate faster than playback. Runs were grouped by model and exclude loading, reference preparation, and the reader pipeline. These are initial measurements of these configurations on this host.</p>
<p class="note">Word errors compare Whisper's transcript with the narration and dialogue scripts. Numbers and names are left for listening because ASR formats them differently. These counts are an intelligibility estimate, not a score for voice quality or accent.</p></div>
<details><summary>How the comparison was made</summary><p>METHOD_DESCRIPTION</p>
<p>Both models receive the same text and reference WAV for each language. Numbers are spelled out. Listening copies use MP3 at 24 kHz and constant volume adjustment toward −20 LUFS, capped at −2 dB true peak. Original WAVs are preserved at their native sample rates. No denoising, time stretching, or dynamic compression is applied.</p>
<p><a href="https://huggingface.co/inclusionAI/Ming-3.0-tts-0.5B">Ming model card</a> · <a href="https://github.com/inclusionAI/Ming-3.0-tts">Ming source</a> · <a href="https://github.com/resemble-ai/chatterbox">Chatterbox source</a></p></details>
<script>
for(const button of document.querySelectorAll('[data-tab]'))button.onclick=()=>{for(const b of document.querySelectorAll('[data-tab]'))b.setAttribute('aria-pressed',b===button);for(const s of document.querySelectorAll('[data-language]'))s.hidden=s.dataset.language!==button.dataset.tab;for(const a of document.querySelectorAll('audio'))a.pause()};
document.getElementById('reveal').onclick=function(){const reveal=this.getAttribute('aria-pressed')!=='true';this.setAttribute('aria-pressed',reveal);this.textContent=reveal?'Hide models and measurements':'Reveal models and measurements';for(const e of document.querySelectorAll('.identity'))e.hidden=!reveal};
for(const a of document.querySelectorAll('audio'))a.addEventListener('play',()=>{for(const b of document.querySelectorAll('audio'))if(a!==b)b.pause()});
</script></html>'''
    page = page.replace('loading, reference preparation, and the reader pipeline', exclusions)
    page = page.replace('METHOD_DESCRIPTION', escape(method))
    if args.timing_note:
        page = page.replace('<h3>What this run measured</h3>',
                            '<h3>What this run measured</h3><p>' + escape(args.timing_note) + '</p>')
    page = page.replace('https://huggingface.co/inclusionAI/Ming-3.0-tts-0.5B', model_url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    args.out.with_suffix('.json').write_text(json.dumps(dict(summary=summary, volume_gain_db=gains,
                                                           timing_note=args.timing_note), indent=2) + '\n')
    overview = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
                '<style>*{box-sizing:border-box}body{margin:0;font:14px/1.5 var(--font-sans,system-ui);color:var(--foreground,CanvasText);background:transparent}',
                'h3{font-size:17px;margin:0 0 10px}p{margin:8px 0 14px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}',
                '.take{min-width:0;background:var(--card,Canvas);border:1px solid var(--border,#888);border-radius:var(--radius,10px);padding:16px}',
                'audio{width:100%;height:38px;margin:12px 0}section{padding:18px 0;border-bottom:1px solid var(--border,#888)}',
                '.note,small{color:var(--muted-foreground,GrayText)}small{display:block}@media(max-width:500px){.pair{grid-template-columns:1fr}}</style>',
                '<p>Matched narration from your saved me-read voices. Playback copies have matched volume. The full listening page also includes dialogue, pronunciation, reference recordings, and a blind mode.</p>']
    if args.timing_note:
        overview.append('<p class="note">' + escape(args.timing_note) + '</p>')
    for language, title in [('en', 'English'), ('es', 'Español')]:
        rows = [r for r in analysis if r['case'] == f'{language}-narration']
        overview.append(f'<section><h3>{title}</h3><p>{escape(rows[0]["text"])}</p><div class="pair">')
        for engine in ('ming', 'chatterbox'):
            row = next(r for r in rows if r['engine'] == engine)
            stats = next(r for r in summary if r['engine'] == engine and r['language'] == language)
            label = ming_label if engine == 'ming' else ('Chatterbox Turbo' if language == 'en' else 'Chatterbox Multilingual')
            overview.append(f'<div class="take"><strong>{label}</strong><audio controls preload="metadata" src="{audio_urls[row["file"]]}"></audio>'
                            f'<small>{stats["generation_median"]:.2f}s work for {stats["audio_seconds"]:.2f}s audio</small>'
                            f'<small>{stats["rtf_median"]:.2f}s work / audio second · range {stats["rtf_min"]:.2f}–{stats["rtf_max"]:.2f}</small></div>')
        overview.append('</div></section>')
    overview.append(f'<p class="note">Median of five warm narration runs on your {hardware}. Each model ran alone. Timings exclude {exclusions}. Runs were grouped by model. These are initial measurements of these configurations, not a general speed ranking.</p>')
    scores = '; '.join(f'{r["language"].upper()} {"Ming" if r["engine"] == "ming" else "Chatterbox"}: '
                       f'{r["word_errors"]}/{r["reference_words"]}' for r in summary)
    overview.append(f'<p class="note">Whisper word errors across narration and dialogue: {scores}. '
                    'A recognizer can make mistakes too; this is not a voice-quality score.</p>')
    overview.append('''<script>
const audios=[...document.querySelectorAll('audio')];
for(const a of audios)a.addEventListener('play',()=>{for(const b of audios)if(a!==b)b.pause()});
Promise.all(audios.map(a=>new Promise((resolve,reject)=>{if(a.readyState>=1)return resolve(a.duration);a.addEventListener('loadedmetadata',()=>resolve(a.duration),{once:true});a.addEventListener('error',()=>reject(a.error),{once:true});}))).then(d=>console.log(JSON.stringify({audioCount:d.length,durations:d}))).catch(e=>console.error('Audio check failed',e));
</script></html>''')
    args.out.with_name('overview.html').write_text(''.join(overview))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
