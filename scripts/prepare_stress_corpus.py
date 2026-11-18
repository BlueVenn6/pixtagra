"""Download distinct COCO test images for local GUI stress; never uploads them.

Dataset images are test inputs, not product assets. The manifest retains source
URLs and license references. No images are included in the release package.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import zipfile

import requests
from PIL import Image

BUCKET = "https://s3.amazonaws.com/images.cocodataset.org"


def prepare(output, count):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "image_info_test2017.zip"
    if not archive.exists():
        response = requests.get(BUCKET + "/annotations/image_info_test2017.zip", timeout=(15, 60))
        response.raise_for_status()
        archive.write_bytes(response.content)
    with zipfile.ZipFile(archive) as z:
        data = json.loads(z.read("annotations/image_info_test2017.json"))

    def fetch(record):
        target = output / record["file_name"]
        url = BUCKET + "/test2017/" + record["file_name"]
        if not target.exists():
            for attempt in range(3):
                try:
                    response = requests.get(url, timeout=(30, 45))
                    response.raise_for_status()
                    content = response.content
                    break
                except requests.RequestException:
                    if attempt == 2:
                        return None
            with Image.open(io.BytesIO(content)) as image:
                image.load()
            target.write_bytes(content)
        content = target.read_bytes()
        with Image.open(target) as image:
            image.load()
            rgb = image.convert("RGB")
            pixels_hash = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
            width, height = image.size
        return {"file": target.name, "source_url": url, "license": record.get("license"),
                "sha256": hashlib.sha256(content).hexdigest(), "pixels_sha256": pixels_hash,
                "width": width, "height": height, "bytes": len(content)}

    candidates = sorted(data["images"], key=lambda record: record["id"])
    records, seen = [], set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for offset in range(0, len(candidates), count):
            for record in pool.map(fetch, candidates[offset:offset + count]):
                if record is not None and record["pixels_sha256"] not in seen:
                    seen.add(record["pixels_sha256"])
                    records.append(record)
                if len(records) == count:
                    break
            if len(records) == count:
                break
    if len(records) != count:
        raise RuntimeError("Insufficient distinct decoded images")
    manifest = {"dataset": "COCO test2017", "images_uploaded_to_api": False,
                "annotation_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "unique_decoded_images": len(seen), "licenses": data.get("licenses", []),
                "images": records}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(records), "bytes": sum(r["bytes"] for r in records),
                      "manifest": str(output / "manifest.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.count <= 5000:
        parser.error("count must be between 1 and 5000")
    prepare(args.output, args.count)
