# Run speech and pictures on a GPU machine

me-read runs on a Mac by itself. A Linux machine with a GPU can add two things:

- **Speech.** The [speech service](../speech-service/README.md) designs voices, makes emotion takes, compares speakers, and narrates with Chatterbox or Breeze. The Mac only transcribes voices with Whisper.
- **Pictures.** The [image service](../imager/README.md) draws a portrait of each character and the key moments of the book with FLUX.2 klein 4B.

Both were built and tested on an AMD Radeon RX 6900 XT with 16 GB and ROCm. They cannot share a 16 GB card, so the GPU machine does one or the other at a time.

## Install the services

On the GPU machine, follow the setup in [the speech service README](../speech-service/README.md), [the image service README](../imager/README.md), or both. Install each one as a systemd user service, as its README shows. The two unit files conflict with each other, so starting one stops the other.

On the Mac, check that SSH reaches the machine without a password prompt:

```sh
ssh my-gpu-box true
```

Replace `my-gpu-box` with the machine's SSH name, here and below.

## Start me-read with the GPU machine

Pass the machine's SSH name once:

```sh
./start --gpu-host my-gpu-box
```

`./start` then does the following:

1. It builds the web app.
2. It starts `me-read-speech` on the GPU machine. That stops `me-read-imager` if it was running.
3. It opens an SSH tunnel from `127.0.0.1:8781` on the Mac to the service.
4. It waits until the service is ready, then starts the reader.

The first time, it asks for your own statement that you may use the reference voices. The speech service requires one with every request that uses a voice. me-read never writes one for you.

`./start` saves the SSH name and your statement in `launcher.json` in your library folder, readable only by your user. Later, a plain `./start` uses the GPU machine again.

Press Ctrl-C to stop the reader and close the tunnel. The service on the GPU machine keeps running. If the tunnel drops, the reader stops too. Run `./start` again to reconnect.

### Draw pictures instead

```sh
./start --gpu images
```

This starts `me-read-imager` instead, which stops the speech service, and tunnels it to `127.0.0.1:8766`. Speech runs on the Mac while the GPU draws. The library's image address is set to the tunnel. If you turned pictures off earlier, click **Pictures off** in the reader to turn them on.

The next plain `./start` switches the GPU machine back to speech and clears the image address, so the reader does not queue pictures for a stopped service.

### Change or forget the machine

- **Use another machine.** Pass `--gpu-host other-box`. It replaces the saved one.
- **Run everything on the Mac once.** Pass `--gpu local`. The saved machine stays saved.
- **Forget the machine.** Pass `--forget-gpu-host`.

`./start` checks that the reader and tunnel ports are free before it switches any service on the GPU machine.

## Connect to a service yourself

To manage the service and tunnel yourself, give its address and your statement directly:

```sh
ssh -N -L 8781:127.0.0.1:8781 my-gpu-box
```

In another terminal:

```sh
./start --tts-url http://127.0.0.1:8781 --tts-consent "YOUR OWN STATEMENT"
```

Replace `YOUR OWN STATEMENT` with your own words. With `--tts-url`, `./start` does not use SSH and does not change the image address.

For an image service you manage yourself, start with `./start --gpu local` and enter its address in the library's settings, for example `http://127.0.0.1:8766`.

## What the speech service does

| Work | Model on the service |
|---|---|
| Design voices for English books | Breeze |
| Design voices for other books | Qwen |
| Emotion takes | Breeze |
| Tell a new voice apart from the cast | Chatterbox's speaker model |
| Narrate with `chatterbox` | Chatterbox Turbo for English, Chatterbox Multilingual for other languages |
| Narrate with `breeze-gguf` | Breeze, English only |

With `fish` or `breeze` as the narrator, narration stays on the Mac and the service does the rest.

To narrate with Breeze on the service:

```sh
./start --tts-provider breeze-gguf
```

Things to know:

- `chatterbox` on the Mac and on the service count as the same narrator, so clips that are ready stay ready when you switch. The service runs the full Chatterbox model rather than the Mac's 8-bit copy, so new clips may sound slightly different.
- me-read asks the service to leave the spoken AI disclaimer out of every request. You are then responsible for telling listeners the audio is AI-generated. me-read refuses audio unless the service confirms it left the disclaimer out.
- The service does one request at a time and loads each model on first use. While it is down or loading, me-read waits 15 seconds and tries again. Nothing fails, and nothing falls back to the Mac.
- A request the service rejects fails that voice, take, or clip with the service's error.
- Text and voices travel over plain HTTP with no login. Use the SSH tunnel or a network you trust.
- me-read names each uploaded voice after the SHA-256 of its WAV bytes, so a replaced voice never reuses an old one.
