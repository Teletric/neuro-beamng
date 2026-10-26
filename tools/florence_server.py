"""Vision server for the bridge's --vision feature, using Microsoft Florence-2.

Speaks the same OpenAI-style /v1/chat/completions API the bridge already uses, so it is a
drop-in replacement for a llama.cpp vision model. Each answer is a short caption plus the
objects Florence detects, with where they are in view. Run with the Florence venv:

    .venv-florence\\Scripts\\python tools\\florence_server.py --model models\\Florence-2-base

Python 3.8 compatible (it runs on the PyTorch already installed there).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import torch
from PIL import Image
from transformers import AutoModelForCausalLM, AutoProcessor
from transformers.dynamic_module_utils import get_imports


def _imports_without_flash_attn(filename):
    # Florence's code imports flash_attn unconditionally, but only needs it when asked to use it.
    return [i for i in get_imports(filename) if i != "flash_attn"]


def where(box, w: float, h: float) -> tuple[str, str]:
    """Horizontal position and rough size of a [x1, y1, x2, y2] box."""
    cx = (box[0] + box[2]) / 2 / w
    pos = ("far left" if cx < 0.2 else "left" if cx < 0.4 else "center" if cx < 0.6
           else "right" if cx < 0.8 else "far right")
    area = (box[2] - box[0]) * (box[3] - box[1]) / (w * h)
    size = "big/near" if area > 0.15 else "medium" if area > 0.03 else "small/far"
    return pos, size


def is_own_car(label: str, box, w: float, h: float) -> bool:
    """With the chase camera the player's car fills the lower middle of the frame, so anything
    centred there (the car, its wheels, plate, lights) is hers. Things far ahead sit higher up,
    near the horizon."""
    cx = (box[0] + box[2]) / 2 / w
    cy = (box[1] + box[3]) / 2 / h
    big_vehicle = any(v in label for v in ("car", "vehicle", "truck", "van", "bus", "suv")) \
        and 0.3 < cx < 0.7 and box[3] / h > 0.6 and (box[2] - box[0]) / w > 0.2
    return big_vehicle or (0.25 < cx < 0.75 and cy > 0.5)


def main() -> None:
    p = argparse.ArgumentParser(description="Florence-2 vision server (OpenAI-style API).")
    p.add_argument("--model", default=os.path.join(os.path.dirname(__file__), "..", "models", "Florence-2-base"))
    p.add_argument("--port", type=int, default=11435)
    p.add_argument("--cpu", action="store_true", help="run on the CPU instead of the GPU")
    args = p.parse_args()

    device = "cpu" if args.cpu or not torch.cuda.is_available() else "cuda"
    dtype = torch.float16 if device == "cuda" else torch.float32
    print(f"Loading Florence-2 from {args.model} on {device} ...", flush=True)
    t0 = time.monotonic()
    with mock.patch("transformers.dynamic_module_utils.get_imports", _imports_without_flash_attn):
        model = AutoModelForCausalLM.from_pretrained(
            args.model, trust_remote_code=True, torch_dtype=dtype, attn_implementation="eager").to(device).eval()
        processor = AutoProcessor.from_pretrained(args.model, trust_remote_code=True)
    print(f"Loaded in {time.monotonic() - t0:.1f}s", flush=True)
    lock = threading.Lock()

    def run(task: str, image: Image.Image):
        inputs = processor(text=task, images=image, return_tensors="pt").to(device, dtype)
        with torch.inference_mode():
            ids = model.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                                 max_new_tokens=256, num_beams=3, do_sample=False)
        text = processor.batch_decode(ids, skip_special_tokens=False)[0]
        return processor.post_process_generation(text, task=task, image_size=image.size)[task]

    def describe(image: Image.Image) -> str:
        w, h = image.size
        with lock:
            caption = run("<CAPTION>", image).strip()
            od = run("<OD>", image)
        seen = []
        for label, box in zip(od.get("labels", []), od.get("bboxes", [])):
            label = label.lower().strip()
            if is_own_car(label, box, w, h) or "plate" in label:  # the plate in view is always hers
                continue
            pos, size = where(box, w, h)
            item = f"{label} ({pos}, {size})"
            if item not in seen:
                seen.append(item)
        objects = "; ".join(seen[:8]) if seen else "nothing distinct"
        return f"{caption} Objects in view: {objects}."

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:
            pass

        def _reply(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") in ("/health", "/v1/models"):
                self._reply(200, {"status": "ok", "data": [{"id": "florence-2", "object": "model"}]})
            else:
                self._reply(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/v1/chat/completions":
                self._reply(404, {"error": {"message": "not found"}})
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                url = next(part["image_url"]["url"] for msg in req["messages"] for part in msg["content"]
                           if isinstance(part, dict) and part.get("type") == "image_url")
                image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
                t = time.monotonic()
                text = describe(image)
                print(f"  {time.monotonic() - t:.2f}s: {text}", flush=True)
                self._reply(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})
            except Exception as e:  # report to the client instead of dropping the connection
                self._reply(500, {"error": {"message": f"{type(e).__name__}: {e}"}})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Florence-2 vision server at http://127.0.0.1:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
