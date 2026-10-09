# imager

The image service for me-read's illustrations. It keeps FLUX.2 klein 4B loaded on a GPU and draws one picture per request. It runs on a second machine so that drawing does not compete with speech on the Mac.

Written for and tested on Linux with an AMD Radeon RX 6900 XT (16 GB) through the ROCm build of PyTorch. There a 512x512 portrait takes about 9 seconds and a 640x432 scene with two reference portraits about 12, and the 896x608 scene the reader asks for about 19.

## Run it

Needs [uv](https://docs.astral.sh/uv/) and a user in the `video` and `render` groups. The PyTorch wheels bring their own ROCm libraries.

```sh
uv run --frozen python server.py
```

The first start downloads about 16 GB of model files into `~/.cache/huggingface`, and loading takes under a minute after that. The service listens on port 8766 on every interface; `--host` and `--port` change that. It has no login, so anyone who can reach the port can use the GPU: keep it on a network you trust.

Enter `http://<this machine>:8766` as the image service URL in me-read's settings.

To keep it running across logins and reboots, install it as a user service. The unit expects this directory at `~/me-read-imager`.

```sh
cp me-read-imager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now me-read-imager
loginctl enable-linger "$USER"
```

If a firewall is on, open the port to your network, for example with ufw:

```sh
sudo ufw allow from 192.168.100.0/24 to any port 8766 proto tcp
```

## API

- `GET /health` answers `{"ready": true, "model": "...", "error": null}`. `ready` is false while the model loads.
- `POST /generate` takes `{"prompt", "width", "height", "seed", "references", "reference_px"}` and answers with a PNG. `references` are base64 encoded images of the people in the scene, in the order the prompt refers to them ("the person in image 1"), at most four; each is shrunk to `reference_px` square. Width and height are multiples of 16 between 256 and 1024. It answers 503 while the model loads.

## Other cards

The 4-bit transformer (`TRANSFORMER` in `server.py`) is chosen so that it fits next to the text encoder in 16 GB. For an NVIDIA card, point the `pytorch-rocm` index in `pyproject.toml` at a CUDA build of PyTorch and drop `pytorch-triton-rocm`; this has not been tried.
