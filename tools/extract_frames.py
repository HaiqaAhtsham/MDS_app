"""Dump frames from drone videos for labelling a YOLO training set.

    python tools/extract_frames.py videos/*.mp4 --every 0.5 --out dataset/images

Label the frames (e.g. with Label Studio or CVAT, YOLO format), train with
    pip install ultralytics
    yolo detect train data=dataset.yaml model=yolov8n.pt imgsz=1280 epochs=100
and copy runs/detect/train/weights/best.pt to models/detector.pt. The app
then uses it instead of template matching.
"""

import argparse
from pathlib import Path

import cv2


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="+", type=Path)
    ap.add_argument("--every", type=float, default=1.0, help="seconds between saved frames")
    ap.add_argument("--out", type=Path, default=Path("dataset/images"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for video in args.videos:
        cap = cv2.VideoCapture(str(video))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, int(round(fps * args.every)))
        idx = saved = 0
        while cap.grab():
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    cv2.imwrite(str(args.out / f"{video.stem}_{idx:06d}.jpg"), frame)
                    saved += 1
            idx += 1
        cap.release()
        print(f"{video.name}: saved {saved} frames")


if __name__ == "__main__":
    main()
