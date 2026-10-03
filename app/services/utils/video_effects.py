from typing import Any

import numpy as np
from moviepy import ColorClip, CompositeVideoClip, vfx
from PIL import Image


# FadeIn
def fadein_transition(clip: Any, t: float) -> Any:
    return clip.with_effects([vfx.FadeIn(t)])


# FadeOut
def fadeout_transition(clip: Any, t: float) -> Any:
    return clip.with_effects([vfx.FadeOut(t)])


# SlideIn
def slidein_transition(clip: Any, t: float, side: str) -> Any:
    width, height = clip.size

    # MoviePy's built-in SlideIn is unstable for full-screen materials in the current processing chain.
    # There will be a situation where "the transition is logically applied, but there is almost no change in the picture."
    # Here it is changed to explicit black background + displacement animation to ensure that the transition effect is visible and the behavior is controllable.
    def position(current_time: float):
        progress = min(max(current_time / max(t, 0.001), 0), 1)

        if side == "left":
            return (-width + width * progress, 0)
        if side == "right":
            return (width - width * progress, 0)
        if side == "top":
            return (0, -height + height * progress)
        if side == "bottom":
            return (0, height - height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# SlideOut
def slideout_transition(clip: Any, t: float, side: str) -> Any:
    width, height = clip.size
    transition_start = max(clip.duration - t, 0)

    # SlideOut is also changed to explicit displacement to ensure that the end of the clip can slide out of the screen stably.
    def position(current_time: float):
        if current_time <= transition_start:
            return (0, 0)

        progress = min(
            max((current_time - transition_start) / max(t, 0.001), 0), 1
        )

        if side == "left":
            return (-width * progress, 0)
        if side == "right":
            return (width * progress, 0)
        if side == "top":
            return (0, -height * progress)
        if side == "bottom":
            return (0, height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# Retaining the 20% zoom range of the original design gives a clearly visible sense of Ken Burns' movement even in short clips of around three seconds.
# Scaling stability is ensured by sub-pixel center sampling below, without masking source video encoding flicker by reducing the magnitude of the effect.
_ZOOM_MAX_SCALE = 1.2


def _zoom_frame(frame: np.ndarray, scale_factor: float) -> np.ndarray:
    """Use sub-pixel center cropping to achieve black-edge-free and stable zoom effects.

    You cannot first convert the cropping width and height into integers: when the scaling ratio changes continuously, the integer boundaries will jump at different steps.
    And when switching between odd and even sizes, the half-pixel sampling phase is changed, which ultimately manifests as screen jitter. Pillow's EXTENT
    The transformation can directly receive floating point boundaries and complete sub-pixel sampling on the fixed output canvas; left and right, upper and lower boundaries
    It is always symmetrical around the same floating point center, so it is suitable for scenes where the entire video continues to zoom slowly.
    """
    if scale_factor <= 0:
        raise ValueError("scale_factor must be greater than zero")

    # 1x zoom directly returns to the original frame to avoid meaningless resampling causing slight blurring of the first frame.
    if abs(scale_factor - 1.0) < 1e-9:
        return frame

    height, width = frame.shape[:2]
    crop_width = width / scale_factor
    crop_height = height / scale_factor
    left = (width - crop_width) / 2
    top = (height - crop_height) / 2
    right = left + crop_width
    bottom = top + crop_height

    image = Image.fromarray(frame)
    transformed = image.transform(
        (width, height),
        Image.Transform.EXTENT,
        (left, top, right, bottom),
        # Continuous video scaling pays more attention to the consistency of adjacent frames. BICUBIC/LANCZOS Although single frame is sharper,
        # However, high-frequency textures are prone to ringing and brightness flickering when crossing the sampling grid; BILINEAR is softer and
        # A small loss of sharpness can be exchanged for a more stable dynamic look.
        resample=Image.Resampling.BILINEAR,
    )
    return np.asarray(transformed)


def zoomin_transition(clip: Any, t: float) -> Any:
    """Smoothly zoom in from original to 1.2x across the entire clip."""
    # t is temporarily reserved to maintain a unified call signature with other transition functions; scaling needs to cover the entire clip,
    # Otherwise, the picture will suddenly freeze after a short zoom, which is not suitable for static or low-motion materials.
    _ = t
    duration = max(clip.duration, 0.001)

    def scale_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        scale_factor = 1 + (_ZOOM_MAX_SCALE - 1) * progress
        return _zoom_frame(get_frame(current_time), scale_factor)

    return clip.transform(scale_effect)


def zoomout_transition(clip: Any, t: float) -> Any:
    """Smoothly zoom out from 1.2x to original frame throughout the clip."""
    # Consistent with zoomin_transition, t is only used to be compatible with the unified transition calling interface.
    _ = t
    duration = max(clip.duration, 0.001)

    def scale_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        scale_factor = _ZOOM_MAX_SCALE - (_ZOOM_MAX_SCALE - 1) * progress
        return _zoom_frame(get_frame(current_time), scale_factor)

    return clip.transform(scale_effect)


_PAN_MAX_SCALE = 1.15


def _pan_frame(
    frame: np.ndarray,
    progress: float,
    direction: str = "left",
    scale_factor: float = _PAN_MAX_SCALE,
) -> np.ndarray:
    """Pan a frame smoothly across its canvas using sub-pixel interpolation without black borders.

    progress: float from 0.0 to 1.0
    direction: 'left', 'right', 'up', 'down'
    scale_factor: slight overshoot scale (default 1.15) to allow panning room without black borders
    """
    if scale_factor <= 1.0:
        scale_factor = 1.05

    progress = min(max(progress, 0.0), 1.0)
    height, width = frame.shape[:2]
    crop_width = width / scale_factor
    crop_height = height / scale_factor

    max_dx = max(width - crop_width, 0.0)
    max_dy = max(height - crop_height, 0.0)

    if direction == "left":
        # Image moves slowly to the left (viewing window starts at 0 and pans right)
        left = max_dx * progress
        top = (height - crop_height) / 2
    elif direction == "right":
        # Image moves slowly to the right (viewing window starts at max_dx and pans left)
        left = max_dx * (1.0 - progress)
        top = (height - crop_height) / 2
    elif direction == "up":
        left = (width - crop_width) / 2
        top = max_dy * progress
    elif direction == "down":
        left = (width - crop_width) / 2
        top = max_dy * (1.0 - progress)
    else:
        left = (width - crop_width) / 2
        top = (height - crop_height) / 2

    right = left + crop_width
    bottom = top + crop_height

    image = Image.fromarray(frame)
    transformed = image.transform(
        (width, height),
        Image.Transform.EXTENT,
        (left, top, right, bottom),
        resample=Image.Resampling.BILINEAR,
    )
    return np.asarray(transformed)


def _pan_zoom_frame(
    frame: np.ndarray,
    progress: float,
    direction: str = "left",
    start_scale: float = 1.10,
    end_scale: float = 1.25,
) -> np.ndarray:
    """Pan across the frame while smoothly zooming in."""
    progress = min(max(progress, 0.0), 1.0)
    current_scale = start_scale + (end_scale - start_scale) * progress
    return _pan_frame(frame, progress, direction=direction, scale_factor=current_scale)


def pan_left_transition(clip: Any, t: float = 0) -> Any:
    """Smoothly pan across the entire clip to the left (slow pan left)."""
    _ = t
    duration = max(clip.duration, 0.001)

    def pan_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        return _pan_frame(get_frame(current_time), progress, direction="left")

    return clip.transform(pan_effect)


def pan_right_transition(clip: Any, t: float = 0) -> Any:
    """Smoothly pan across the entire clip to the right (slow pan right)."""
    _ = t
    duration = max(clip.duration, 0.001)

    def pan_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        return _pan_frame(get_frame(current_time), progress, direction="right")

    return clip.transform(pan_effect)


def pan_left_zoom_transition(clip: Any, t: float = 0) -> Any:
    """Smoothly pan left while zooming in slightly across the entire clip."""
    _ = t
    duration = max(clip.duration, 0.001)

    def pan_zoom_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        return _pan_zoom_frame(get_frame(current_time), progress, direction="left")

    return clip.transform(pan_zoom_effect)


def pan_right_zoom_transition(clip: Any, t: float = 0) -> Any:
    """Smoothly pan right while zooming in slightly across the entire clip."""
    _ = t
    duration = max(clip.duration, 0.001)

    def pan_zoom_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        return _pan_zoom_frame(get_frame(current_time), progress, direction="right")

    return clip.transform(pan_zoom_effect)

