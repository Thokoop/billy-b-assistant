"""List available camera devices and capture a test frame from each."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

print("Scanning camera devices...\n")

found = []
for index in range(10):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        cap.release()
        continue
    # Discard warmup frames
    for _ in range(3):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if ret:
        h, w = frame.shape[:2]
        print(f"  [{index}] {w}x{h}")
        found.append(index)

if not found:
    print("No cameras found.")
else:
    print(f"\nSet CAMERA_DEVICE=<index> in your .env to choose one.")
