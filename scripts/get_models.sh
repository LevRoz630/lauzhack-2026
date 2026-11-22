#!/usr/bin/env bash
# Download the pretrained models into models/ (they are not kept in git).
set -uo pipefail
mkdir -p "$(dirname "$0")/../models"
cd "$(dirname "$0")/../models" || exit 1
base=https://storage.googleapis.com/mediapipe-models
[ -s face_landmarker.task ] || curl -fL -o face_landmarker.task "$base/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
[ -s image_embedder.tflite ] || curl -fL -o image_embedder.tflite "$base/image_embedder/mobilenet_v3_small/float32/1/mobilenet_v3_small.tflite"
[ -s face_recognition_sface.onnx ] || curl -fL -o face_recognition_sface.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
ls -l
