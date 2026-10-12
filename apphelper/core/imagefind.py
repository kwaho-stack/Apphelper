"""핵심 crash 이미지 모드: 화면에서 템플릿 이미지를 찾아 그 중심 좌표를 돌려준다.

정규화 상호상관(ZNCC, 회색조)을 FFT 로 계산한다. 화면은 메모리에서만 읽고 파일로 남기지 않는다.
템플릿은 설정 파일에 zlib 압축 RGB 바이트(base64)로 저장한다.
"""
from __future__ import annotations

import base64
import zlib
from dataclasses import dataclass

import numpy as np

from .crash import ScreenBackend

MIN_SIDE = 4
MAX_SIDE = 400


# ---------------------------------------------------------------- 템플릿 저장 형식

def encode_template(rgb: np.ndarray) -> dict:
    """(h, w, 3) uint8 RGB 배열 → JSON 에 넣을 dict."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = rgb.shape[:2]
    return {"w": int(w), "h": int(h),
            "rgb": base64.b64encode(zlib.compress(rgb.tobytes(), 9)).decode("ascii")}


def decode_template(d: dict) -> np.ndarray:
    w, h = int(d["w"]), int(d["h"])
    raw = zlib.decompress(base64.b64decode(d["rgb"]))
    return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)


def validate_template(rgb: np.ndarray) -> str | None:
    """템플릿으로 쓸 수 없으면 이유를, 쓸 수 있으면 None 을 돌려준다."""
    h, w = rgb.shape[:2]
    if min(h, w) < MIN_SIDE:
        return f"이미지가 너무 작습니다 (최소 {MIN_SIDE}×{MIN_SIDE})"
    if max(h, w) > MAX_SIDE:
        return f"이미지가 너무 큽니다 (최대 {MAX_SIDE}×{MAX_SIDE}). 찾을 부분만 작게 잘라 주세요"
    if float(_gray(rgb).std()) < 1.0:
        return "이미지가 한 가지 색이라 위치를 찾을 수 없습니다. 모양이 보이게 잘라 주세요"
    return None


# ---------------------------------------------------------------- 화면 읽기

def grab_screen(backend: ScreenBackend) -> np.ndarray:
    """주 모니터 전체를 (H, W, 3) RGB 배열로 읽는다."""
    w, h = backend.screen_size()
    grab_array = getattr(backend, "grab_array", None)
    if grab_array is not None:
        return grab_array(0, 0, w, h)
    return np.asarray(backend.grab(0, 0, w, h), dtype=np.uint8).reshape(h, w, 3)


# ---------------------------------------------------------------- 찾기

@dataclass(frozen=True)
class Match:
    x: int        # 중심 좌표
    y: int
    score: float  # 0~1 (1 = 완전히 같음)


def _gray(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.float64)
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _window_sum(a: np.ndarray, h: int, w: int) -> np.ndarray:
    """모든 (h, w) 창의 합 (valid 영역)."""
    c = np.zeros((a.shape[0] + 1, a.shape[1] + 1))
    c[1:, 1:] = a.cumsum(0).cumsum(1)
    return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]


def _fast_len(n: int) -> int:
    """n 이상인 가장 작은 2·3·5 곱 (FFT 가 빠른 크기)."""
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            m = p35
            while m < n:
                m *= 2
            best = min(best, m)
            p35 *= 3
        p5 *= 5
    return best


def _shrink(a: np.ndarray, f: int) -> np.ndarray:
    """f×f 평균으로 축소."""
    h, w = a.shape[0] // f * f, a.shape[1] // f * f
    return a[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))


def _zncc(s: np.ndarray, t: np.ndarray) -> np.ndarray | None:
    """회색조 화면 s 의 모든 위치에 대한 템플릿 t 의 ZNCC 점수 (valid 영역)."""
    H, W = s.shape
    h, w = t.shape
    if h > H or w > W:
        return None
    t = t - t.mean()
    t_norm = np.sqrt((t * t).sum())
    if t_norm == 0:
        return None
    # 분자: 화면과 (평균을 뺀) 템플릿의 상호상관. 템플릿이 0 평균이라 화면 창 평균은 빼지 않아도 된다.
    fh, fw = _fast_len(H + h - 1), _fast_len(W + w - 1)
    num = np.fft.irfft2(np.fft.rfft2(s, (fh, fw)) * np.fft.rfft2(t[::-1, ::-1], (fh, fw)), (fh, fw))
    num = num[h - 1:H, w - 1:W]
    n = h * w
    win = _window_sum(s, h, w)
    win2 = _window_sum(s * s, h, w)
    den = np.sqrt(np.maximum(win2 - win * win / n, 0)) * t_norm
    return np.where(den > 1e-6 * max(t_norm, 1), num / np.maximum(den, 1e-12), 0.0)


def _argmax(score: np.ndarray) -> tuple[int, int, float]:
    iy, ix = np.unravel_index(int(np.argmax(score)), score.shape)
    return int(iy), int(ix), float(score[iy, ix])


def best_match(screen: np.ndarray, template: np.ndarray) -> Match | None:
    """screen 안에서 template 과 가장 비슷한 위치. 화면보다 템플릿이 크면 None.

    큰 템플릿은 축소한 화면에서 먼저 후보를 찾고, 원래 크기에서 후보 주변만 다시 맞춰 본다.
    """
    s = _gray(screen)
    t = _gray(template)
    h, w = t.shape
    f = 4 if min(h, w) >= 32 else 2 if min(h, w) >= 12 else 1
    if f > 1:
        coarse = _zncc(_shrink(s, f), _shrink(t, f))
        if coarse is not None:
            # 상위 몇 개 후보를 원래 크기에서 확인한다 (축소로 순위가 바뀌는 경우 대비).
            flat = np.argsort(coarse, axis=None)[-5:][::-1]
            best: tuple[int, int, float] | None = None
            pad = 2 * f
            for idx in flat:
                cy, cx = np.unravel_index(int(idx), coarse.shape)
                y0, x0 = max(cy * f - pad, 0), max(cx * f - pad, 0)
                y1, x1 = min(cy * f + h + pad, s.shape[0]), min(cx * f + w + pad, s.shape[1])
                local = _zncc(s[y0:y1, x0:x1], t)
                if local is None:
                    continue
                ly, lx, sc = _argmax(local)
                if best is None or sc > best[2]:
                    best = (y0 + ly, x0 + lx, sc)
            if best is not None:
                iy, ix, sc = best
                return Match(ix + w // 2, iy + h // 2, min(sc, 1.0))
    score = _zncc(s, t)
    if score is None:
        return None
    iy, ix, sc = _argmax(score)
    return Match(ix + w // 2, iy + h // 2, min(sc, 1.0))


class NotFound(RuntimeError):
    pass


def locate(backend: ScreenBackend, template: np.ndarray, threshold: float) -> Match:
    """화면에서 template 을 찾는다. 일치도가 threshold 미만이면 NotFound."""
    m = best_match(grab_screen(backend), template)
    if m is None:
        raise NotFound("이미지가 화면보다 큽니다")
    if m.score < threshold:
        raise NotFound(f"화면에서 이미지를 찾지 못했습니다 (최고 일치도 {m.score:.0%}, 기준 {threshold:.0%})")
    return m
