"""M3 身份锚点：复用 M3-Agent 的 extract_faces（InsightFace buffalo_l = RetinaFace + ArcFace），在线增量匹配。

可选依赖：`pip install insightface onnxruntime opencv-python-headless tqdm`（Windows 上 insightface 需要 MSVC 编译）。
匹配规则对齐 M3-Agent：VideoGraph.search_img_nodes 的“与锚点已存嵌入的平均余弦相似度 ≥ 阈值”，
过滤阈值取自其 configs/processing_config.json（检测分 0.85、质量分 22）。
"""
from __future__ import annotations

import base64

import numpy as np

from ..schemas import FaceBox


class FaceAnchors:
    def __init__(self, det_threshold: float = 0.85, quality_threshold: float = 22.0, match_threshold: float = 0.3,
                 max_embeddings: int = 10, ctx_id: int = -1):
        import cv2  # noqa: F401  extract_faces 依赖
        from insightface.app import FaceAnalysis

        from ..third_party.m3agent.face_extraction import extract_faces

        self._extract = extract_faces
        self.app = FaceAnalysis(name="buffalo_l")
        self.app.prepare(ctx_id=ctx_id)
        self.det_threshold = det_threshold
        self.quality_threshold = quality_threshold
        self.match_threshold = match_threshold
        self.max_embeddings = max_embeddings
        self.anchors: dict[str, list[np.ndarray]] = {}

    def _match(self, emb: np.ndarray) -> str:
        best, best_sim = None, -1.0
        for aid, embs in self.anchors.items():
            sim = float(np.mean(np.stack(embs) @ emb))
            if sim > best_sim:
                best, best_sim = aid, sim
        if best is None or best_sim < self.match_threshold:
            best = f"face_{len(self.anchors) + 1}"
            self.anchors[best] = []
        if len(self.anchors[best]) < self.max_embeddings:
            self.anchors[best].append(emb)
        return best

    def process(self, frame_ids: list[str], frames: list[np.ndarray]) -> list[FaceBox]:
        from PIL import Image
        import io

        b64 = []
        for img in frames:
            buf = io.BytesIO()
            Image.fromarray(img).save(buf, format="JPEG", quality=90)  # extract_faces 用 cv2.imdecode 解出 BGR
            b64.append(base64.b64encode(buf.getvalue()).decode())
        faces = self._extract(self.app, b64, num_workers=2)
        out = []
        for f in faces:
            if float(f["extra_data"]["face_detection_score"]) < self.det_threshold:
                continue
            if float(f["extra_data"]["face_quality_score"]) < self.quality_threshold:
                continue
            aid = self._match(np.asarray(f["face_emb"], np.float32))
            out.append(FaceBox(frame_id=frame_ids[f["frame_id"]], bbox=tuple(f["bounding_box"]), face_anchor=aid,
                               label=f"P{aid.split('_')[1]}", score=float(f["extra_data"]["face_detection_score"])))
        return out


def draw_labels(img: np.ndarray, boxes: list[FaceBox]) -> np.ndarray:
    """set-of-mark：在帧上画框并标注 P1 / P2，帮助 VLM 绑定人物。"""
    from PIL import Image, ImageDraw

    im = Image.fromarray(img)
    d = ImageDraw.Draw(im)
    for b in boxes:
        x0, y0, x1, y1 = b.bbox
        d.rectangle([x0, y0, x1, y1], outline=(255, 210, 0), width=2)
        d.rectangle([x0, max(0, y0 - 14), x0 + 22, y0], fill=(255, 210, 0))
        d.text((x0 + 2, max(0, y0 - 13)), b.label, fill=(0, 0, 0))
    return np.asarray(im)
